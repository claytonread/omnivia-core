import CryptoKit
import Foundation

/// How a Locate candidate compares with the last version Core captured of the document
/// (design §9.5). The comparison is exact — length and SHA-256 of the whole file — and it
/// proves only that: a changed digest does not prove the file unrelated.
public enum OriginalRelinkComparison: Equatable, Sendable {
    /// Same length and same digest as the last captured version.
    case identical
    /// Different bytes. The person decides whether it is still the same document.
    case differs
    /// Core has not told this Mac what it last captured, so nothing can be compared. This
    /// is not a match: the person still confirms explicitly.
    case noCapturedVersion
}

/// The last version Core captured of a document, as Core reported it.
public struct OriginalRelinkBaseline: Equatable, Sendable {
    public let sourceDigest: String
    public let sourceLengthBytes: UInt64

    public init(sourceDigest: String, sourceLengthBytes: UInt64) {
        self.sourceDigest = sourceDigest
        self.sourceLengthBytes = sourceLengthBytes
    }
}

/// A candidate original staged by the native picker and compared, and nothing more. Staging
/// binds nothing, reserves nothing, publishes nothing and creates no grant: the file was
/// read once, to be compared, and what was read is not kept.
public struct OriginalRelinkCandidate: Equatable, Sendable {
    public let url: URL
    public let fileName: String
    public let linkedDocumentID: String
    public let accessRef: String
    public let byteLength: UInt64
    public let comparison: OriginalRelinkComparison
}

public enum OriginalRelinkError: Error, Equatable {
    /// The file has no established direct route here that a Locate could move: it is not
    /// connected, is a folder member, was never published, or a relink is already owed.
    case notRelinkable
    /// The candidate is a kind of file the watch was not created for, or not one Core reads.
    case unsupportedType
    case unreadable
}

extension DocumentIntakeEnvironment {
    /// Stages the file the person picked for Locate and compares it with what Core last
    /// captured. The comparison is the only read: it is made from the picker's own URL, not
    /// through any grant, and it publishes nothing.
    ///
    /// The candidate must be the kind of file the watch was created for. A watch fixes its
    /// content kind, so a PDF cannot become the original of a text watch by being located.
    public func stageRelink(
        for file: FileSubscription,
        selectedURL: URL,
        baseline: OriginalRelinkBaseline?
    ) throws -> OriginalRelinkCandidate {
        // A relink Core refused for good has finished and left the route held, so another
        // Locate may replace it; one Core has not answered may not be replaced.
        guard accessStore.descriptors().contains(where: {
            $0.accessRef == file.accessRef
                && $0.linkedDocumentID == file.linkedDocumentID
                && $0.route == .directFile
                && ($0.state == .connected
                    || ($0.state == .relinking
                        && !accessStore.hasUnresolvedRelink(accessRef: $0.accessRef)))
                && $0.lifecycle == .active
        }) else { throw OriginalRelinkError.notRelinkable }
        guard ManualCaptureSubmission.contentKind(for: selectedURL) == file.contentKind else {
            throw OriginalRelinkError.unsupportedType
        }
        let bytes: Data
        do {
            bytes = try reader.readStableBytes(
                from: selectedURL,
                maximumBytes: CaptureIntentV3.maximumPayloadBytes
            )
        } catch {
            throw OriginalRelinkError.unreadable
        }
        let digest = "sha256:" + SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        let comparison: OriginalRelinkComparison
        if let baseline {
            comparison = baseline.sourceDigest == digest
                && baseline.sourceLengthBytes == UInt64(bytes.count) ? .identical : .differs
        } else {
            comparison = .noCapturedVersion
        }
        return OriginalRelinkCandidate(
            url: selectedURL,
            fileName: selectedURL.lastPathComponent,
            linkedDocumentID: file.linkedDocumentID,
            accessRef: file.accessRef,
            byteLength: UInt64(bytes.count),
            comparison: comparison
        )
    }

    /// The confirmed Locate behind design §9.5: reserve the route's next generation and
    /// record the intent that tells Core, then pause the watches of the route, as one
    /// decision on the publication gate.
    ///
    /// From here nothing reads either file. The old generation can no longer publish — an
    /// in-flight preparation finds the generation changed at its final validation, and the
    /// whole batch is refused — and the candidate becomes the route's grant only when
    /// `originalRelinkReconciler` has Core's canonical confirmation. The watches stay paused
    /// after that too; resuming is the person's explicit decision.
    ///
    /// The intent is frozen under the workspace and caller the route's published work was
    /// frozen under. `fallback` is the companion's own binding, used only for a route that
    /// has neither a queued entry nor a watch left to name one.
    @discardableResult
    public func relinkSource(
        candidate: OriginalRelinkCandidate,
        fallback: CaptureDeliveryBinding
    ) throws -> OriginalRelinkIntent {
        let (bindings, watches) = frozenBindings(of: candidate.linkedDocumentID)
        return try publicationGate.withLock {
            let intent = try accessStore.reserveRelink(
                accessRef: candidate.accessRef,
                selectedURL: candidate.url,
                binding: bindings[candidate.accessRef] ?? NativeRootAccessBinding(fallback)
            )
            for watch in watches where watch.accessRef == candidate.accessRef {
                // Paused, not stopped: the watch is the person's, and it resumes when they
                // say so. A watch in any other state keeps it.
                _ = try? fileSubscriptions.update(id: watch.subscriptionID) {
                    guard $0.state == .active || $0.state == .needsAccess else { return }
                    $0.state = .paused
                    $0.needsReconciliation = true
                }
            }
            return intent
        }
    }

    /// The relinks Core has not yet confirmed, for the Files tab to say so. A projection: no
    /// bookmark, no path.
    public func pendingRelinks() -> [OriginalRelinkProgress] {
        accessStore.pendingOriginalRelinks().map {
            OriginalRelinkProgress(
                accessRef: $0.accessRef,
                linkedDocumentID: $0.linkedDocumentID,
                generation: $0.generation,
                candidateName: $0.candidateName,
                refusalCode: $0.refusalCode
            )
        }
    }

    /// The reconciler for the intents `relinkSource` records. The composition root owns the
    /// transport, so this environment stays free of any process or CLI.
    public func originalRelinkReconciler(
        transport: any OriginalRelinkTransport
    ) -> OriginalRelinkReconciler {
        OriginalRelinkReconciler(
            accessStore: accessStore,
            subscriptions: fileSubscriptions,
            gate: publicationGate,
            transport: transport
        )
    }
}

/// One relink that Core has not yet confirmed.
public struct OriginalRelinkProgress: Equatable, Sendable {
    public let accessRef: String
    public let linkedDocumentID: String
    public let generation: UInt64
    public let candidateName: String
    /// Set when Core refused for good; the route stays paused.
    public let refusalCode: String?

    public init(
        accessRef: String,
        linkedDocumentID: String,
        generation: UInt64,
        candidateName: String,
        refusalCode: String?
    ) {
        self.accessRef = accessRef
        self.linkedDocumentID = linkedDocumentID
        self.generation = generation
        self.candidateName = candidateName
        self.refusalCode = refusalCode
    }
}
