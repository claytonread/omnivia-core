import Foundation

/// Why Core's copy of a document is not one a "nothing changed" claim may rest on.
///
/// The raw values are what a watch records, so they are stable.
public enum CurrentCaptureShortfall: String, Codable, Equatable, Sendable {
    /// Core holds no successful capture for this document, or this watch holds none to name.
    case noSuccessfulCapture = "no_successful_capture"
    /// The source was removed in Core.
    case sourceRemoved = "source_removed"
    /// Core does not hold a connected original for this document.
    case originalNotConnected = "original_not_connected"
    /// The capture this watch last succeeded with is not in Core's history of the document, or
    /// is not the bytes the file holds now.
    case captureDiffers = "capture_differs"
    /// The capture's retention expired or it was deleted.
    case captureNotRetained = "capture_not_retained"
    /// No searchable version of that capture is published.
    case notRetrievable = "not_retrievable"
}

public enum CurrentCaptureVerdict: Equatable, Sendable {
    /// Current bytes match a retained, authorised, retrieval-ready capture (design §12.1).
    case current
    case notCurrent(CurrentCaptureShortfall)
    /// Core could not be asked, or refused to answer. Nothing is established either way.
    case unconfirmed(CoreHistoryError)
}

/// Asks Core whether the capture a watch last succeeded with can still stand behind "Already up
/// to date" (design §8.5): the same logical document's capture still exists, is authorised and is
/// retrieval-ready. A local hash or an expired receipt cannot establish that, so a watch that
/// has no verifier establishes nothing.
public protocol CurrentCaptureVerifying: Sendable {
    func verify(
        documentID: String,
        captureID: String?,
        observedDigest: String
    ) -> CurrentCaptureVerdict
}

/// The verifier over the governed `document.history` read.
public struct CoreHistoryCurrentCaptureVerifier: CurrentCaptureVerifying {
    private let transport: any CoreDocumentHistoryTransport

    public init(transport: any CoreDocumentHistoryTransport) {
        self.transport = transport
    }

    public func verify(
        documentID: String,
        captureID: String?,
        observedDigest: String
    ) -> CurrentCaptureVerdict {
        switch CoreDocumentHistoryReader(transport: transport).read(documentID: documentID) {
        case let .success(history):
            return Self.verdict(history, captureID: captureID, observedDigest: observedDigest)
        case .failure(.refused(code: "not_found")):
            return .notCurrent(.noSuccessfulCapture)
        case let .failure(error):
            return .unconfirmed(error)
        }
    }

    /// The decision, from one read of the history. Every condition is Core's own statement;
    /// none is inferred from a receipt or a local row.
    static func verdict(
        _ history: CoreDocumentHistory,
        captureID: String?,
        observedDigest: String
    ) -> CurrentCaptureVerdict {
        guard let captureID else { return .notCurrent(.noSuccessfulCapture) }
        guard history.source.sourceState == .active else { return .notCurrent(.sourceRemoved) }
        guard history.source.originalBindingState == .connected else {
            return .notCurrent(.originalNotConnected)
        }
        // Core's current source has to be the capture this watch holds: a newer one is a
        // different version of the file than the bytes in hand can be said to match.
        guard history.source.currentCaptureID == captureID else {
            return .notCurrent(.captureDiffers)
        }
        guard let capture = history.captures.first(where: { $0.captureID == captureID }) else {
            // A bounded view that stopped before the capture proves nothing about it.
            return history.complete ? .notCurrent(.captureDiffers) : .unconfirmed(.unreadable)
        }
        guard capture.sourceDigest == observedDigest else { return .notCurrent(.captureDiffers) }
        guard capture.retentionState == .retained else { return .notCurrent(.captureNotRetained) }
        // Retrieval-ready is a statement about the version retrieval serves: it has to be an
        // extraction of this very capture, and a published one.
        guard history.extraction.retrievalState == .retrievable,
              let current = history.extraction.currentExtractionID,
              history.extractions.first(where: { $0.extractionRevisionID == current })?
                  .captureID == captureID
        else { return .notCurrent(.notRetrievable) }
        return .current
    }
}
